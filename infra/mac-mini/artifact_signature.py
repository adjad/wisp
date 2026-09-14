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
import ctypes
from contextlib import contextmanager


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


@contextmanager
def publication_lock(root):
    root = Path(root)
    info = root.lstat()
    if (not root.is_absolute() or root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700): refused()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        fd = os.open('.install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        if not private_regular(os.fstat(fd)): refused()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root.lstat().st_dev, root.lstat().st_ino) != (info.st_dev,info.st_ino): refused()
        yield directory
    finally:
        if fd is not None: os.close(fd)
        os.close(directory)


def recovery_history(root):
    rows = {}
    for path in sorted(Path(root).glob('.ledger-authorization-*')):
        if not re.fullmatch(r'\.ledger-authorization-[0-9a-f]{64}', path.name): refused()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            if not private_regular(os.fstat(source.fileno())): refused()
            raw = source.read(65537)
            if len(raw) > 65536: refused()
            record = strict_json(raw)
            if set(record) != {'authorization_sha256','desired_sha256','prior_history_sha256'}: refused()
            if any(not hex_value(v,64) for v in record.values()): refused()
            rows[path.name] = digest(raw)
    return digest(canonical(rows))


def ledger_doctor(root):
    values = {}
    for name in ('artifact-releases.json.lock','artifact-releases.json'):
        try: fd = os.open(Path(root)/name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            values[name] = None;continue
        with os.fdopen(fd,'rb') as source:
            if not private_regular(os.fstat(source.fileno())): refused()
            raw = source.read(65537)
            if len(raw)>65536: refused()
            values[name] = raw
    sentinel, ledger = values['artifact-releases.json.lock'],values['artifact-releases.json']
    if sentinel not in (None,b'',b'1'): refused()
    status = 'recovery_required' if sentinel == b'1' and ledger is None else 'initialized' if ledger is not None else 'unused'
    if ledger is not None:
        doc = strict_json(ledger)
        if (set(doc) != {'schema_version','release_sequence','statement_sha256'} or type(doc['schema_version']) is not int or doc['schema_version'] != 1
                or type(doc['release_sequence']) is not int or doc['release_sequence'] <= 0
                or not hex_value(doc['statement_sha256'],64) or sentinel != b'1'): refused()
    return {'schema_version':1,'status':status,'sentinel_sha256':digest(sentinel or b''),
            'recovery_history_sha256':recovery_history(root),
            'ledger_sha256':digest(ledger) if ledger is not None else None,
            'release_sequence':doc['release_sequence'] if ledger is not None else None}


def recover_ledger(root, authorization, authorization_sha256, *, apply=False):
    """Require an independently reviewed high-water bound, never a last receipt."""
    root = Path(root)
    if apply is not True or digest(canonical(authorization)) != authorization_sha256: refused()
    expected = {'schema_version','operation','root','uid','sentinel_sha256','sequence_floor',
                'statement_sha256','independent_high_water_review','nonce','recovery_history_sha256'}
    if (set(authorization) != expected or type(authorization['schema_version']) is not int or authorization['schema_version'] != 1
            or authorization['operation'] != 'recover-ledger' or authorization['root'] != str(root)
            or type(authorization['uid']) is not int or authorization['uid'] != os.getuid()
            or authorization['independent_high_water_review'] is not True
            or type(authorization['sequence_floor']) is not int or authorization['sequence_floor'] < 1
            or not hex_value(authorization['statement_sha256'],64)
            or not hex_value(authorization['nonce'],64)
            or not hex_value(authorization['recovery_history_sha256'],64)): refused()
    desired = canonical({'schema_version':1,'release_sequence':authorization['sequence_floor'],
                         'statement_sha256':authorization['statement_sha256']})
    with publication_lock(root) as directory:
        fd = os.open('artifact-releases.json.lock',os.O_RDWR | os.O_NOFOLLOW,dir_fd=directory)
        try:
            if not private_regular(os.fstat(fd)): refused()
            fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = ledger_doctor(root)
            if state['sentinel_sha256'] != authorization['sentinel_sha256']: refused()
            record = canonical({'authorization_sha256':authorization_sha256,'desired_sha256':digest(desired),
                                'prior_history_sha256':authorization['recovery_history_sha256']})
            consumed = '.ledger-authorization-' + authorization['nonce']
            try: prior_fd = os.open(consumed,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=directory)
            except FileNotFoundError: pass
            else:
                with os.fdopen(prior_fd,'rb') as prior:
                    if not private_regular(os.fstat(prior.fileno())) or prior.read(65537) != record: refused()
                # A consumed intent may acknowledge only the exact still-present
                # completed repair. A later missing ledger is a new incident.
                if state['ledger_sha256'] == digest(desired):
                    return {'schema_version':1,'status':'complete','idempotent':True}
                refused()
            if state['status'] != 'recovery_required': refused()
            if state['recovery_history_sha256'] != authorization['recovery_history_sha256']: refused()
            intent = '.pending-ledger-intent-' + os.urandom(16).hex()
            burn = os.open(intent,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=directory)
            try:
                with os.fdopen(burn,'wb') as output:
                    output.write(record);output.flush();os.fsync(output.fileno())
                rename = ctypes.CDLL(None,use_errno=True).renameatx_np
                rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
                rename.restype = ctypes.c_int
                if rename(directory,intent.encode(),directory,consumed.encode(),4) != 0: refused()
                os.fsync(directory)
            finally:
                try:os.unlink(intent,dir_fd=directory)
                except FileNotFoundError:pass
            name = '.ledger-recovery-'+os.urandom(16).hex()
            output = os.open(name,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=directory)
            try:
                with os.fdopen(output,'wb') as stream:
                    stream.write(desired);stream.flush();os.fsync(stream.fileno())
                rename = ctypes.CDLL(None, use_errno=True).renameatx_np
                rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
                rename.restype = ctypes.c_int
                if rename(directory,name.encode(),directory,b'artifact-releases.json',4) != 0: refused()
                os.fsync(directory)
            finally:
                try: os.unlink(name,dir_fd=directory)
                except FileNotFoundError: pass
        finally: os.close(fd)
    return {'schema_version':1,'status':'complete','idempotent':False}


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
