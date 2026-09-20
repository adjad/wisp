"""Minimal authenticated backend for Wisp Summary QA."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import struct

try:
    from .harness import LiveSummaryRunner, OneShotHarness, QAError, canonical_manifest
except ImportError:
    from service.qa_harness import LiveSummaryRunner, OneShotHarness, QAError, canonical_manifest

HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def read_exact(fd, size):
    result = bytearray()
    while len(result) < size:
        block = os.read(fd, size - len(result))
        if not block:
            raise QAError("credential_pipe_unavailable")
        result.extend(block)
    return result


def consume_credential_pipe():
    metadata = os.environ.pop("WISP_CREDENTIAL_PIPE", "")
    match = re.fullmatch(r"v1:(\d+):(\d+)", metadata)
    try:
        info = os.fstat(0)
    except OSError:
        raise QAError("credential_pipe_unavailable") from None
    if (match is None or not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) != (int(match.group(1)), int(match.group(2)))):
        raise QAError("credential_pipe_unavailable")
    header, body = read_exact(0, 12), bytearray()
    try:
        if bytes(header[:8]) != b"WISPCP1\n":
            raise QAError("credential_pipe_unavailable")
        length = struct.unpack(">I", bytes(header[8:]))[0]
        if not 1 <= length <= 4096:
            raise QAError("credential_pipe_unavailable")
        body = read_exact(0, length)
        frame = json.loads(body)
        credentials = frame.get("credentials") if isinstance(frame, dict) else None
        generation = frame.get("generation") if isinstance(frame, dict) else None
        if (not isinstance(frame, dict)
                or set(frame) != {"version", "pid", "uid", "role", "generation", "credentials"}
                or frame["version"] != 1 or frame["pid"] != os.getpid()
                or frame["uid"] != os.getuid() or frame["role"] != "primary"
                or not (generation == "absent"
                        or isinstance(generation, str) and HEX64.fullmatch(generation))
                or not isinstance(credentials, dict)
                or set(credentials) != {"WISP_LOCAL_OMLX_KEY"}
                or not isinstance(credentials["WISP_LOCAL_OMLX_KEY"], str)
                or not HEX64.fullmatch(credentials["WISP_LOCAL_OMLX_KEY"])):
            raise QAError("credential_pipe_unavailable")
        return credentials["WISP_LOCAL_OMLX_KEY"]
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise QAError("credential_pipe_unavailable") from None
    finally:
        header[:] = b"\0" * len(header)
        body[:] = b"\0" * len(body)
        try:
            os.close(0)
        except OSError:
            pass


def verify_staged_sources(stage, candidate_sha):
    try:
        build = json.loads((stage / "qa-build-manifest.json").read_text())
        hashes = build.get("staged_hashes")
        if build.get("candidate_sha") != candidate_sha or not isinstance(hashes, dict) or not hashes:
            return False
        root = stage.resolve(strict=True)
        for relative, expected in hashes.items():
            path = stage / relative
            info = path.lstat()
            if (not isinstance(relative, str) or not HEX64.fullmatch(str(expected))
                    or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o022
                    or not path.resolve(strict=True).is_relative_to(root)
                    or sha(path) != expected):
                return False
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def derive_capability(key, candidate_sha, label):
    return hmac.new(bytes.fromhex(key),
                    f"wisp-summary-qa:{label}:{candidate_sha}".encode(),
                    hashlib.sha256).hexdigest()


class ManagedQABackend:
    def __init__(self, harness, manifest_id, capability):
        self.harness, self.manifest_id, self.capability = harness, manifest_id, capability

    async def post(self, body, run, readiness):
        if (not isinstance(body, dict) or set(body) != {"manifest_id", "capability"}
                or body.get("manifest_id") != self.manifest_id
                or not isinstance(body.get("capability"), str)):
            raise QAError("invalid_request")
        return await self.harness.consume(
            hmac.compare_digest(body["capability"], self.capability), readiness, run)


class QAASGIApp:
    def __init__(self):
        self.backend = self.runner = None
        self.challenge = ""
        self.readiness = {"exclusive_session": False, "model_loaded": False, "server_idle": False}
        self.qualified = False
        candidate = os.environ.get("WISP_QA_CANDIDATE_SHA", "")
        stage = Path(__file__).resolve().parents[1]
        manifest_path = Path(__file__).with_name("qa-manifest.json")
        if not HEX40.fullmatch(candidate) or not manifest_path.is_file():
            return
        try:
            credential = consume_credential_pipe()
            manifest, digest = canonical_manifest(manifest_path)
            valid = verify_staged_sources(stage, candidate)
            if not valid:
                raise QAError("source_hash_mismatch")
            self.challenge = derive_capability(credential, candidate, "ready")
            run_capability = derive_capability(credential, candidate, "run")
            self.runner = LiveSummaryRunner(
                manifest, credential, stage / "service", source_hashes_valid=valid)
            self.backend = ManagedQABackend(
                OneShotHarness(manifest, digest, candidate), manifest["manifest_id"], run_capability)
        except Exception:
            self.backend = self.runner = None

    async def qualify(self):
        if not self.qualified and self.runner is not None:
            self.readiness = await self.runner.qualify()
            self.qualified = True

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        method, path = scope.get("method"), scope.get("path")
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if method == "GET" and path == "/qa/ready":
            supplied = headers.get(b"x-wisp-qa-challenge", b"")
            valid = (self.backend is not None and len(supplied) == 64
                     and hmac.compare_digest(supplied.decode("ascii", "ignore"), self.challenge))
            if not valid:
                return await self.reply(send, 403, {"ready": False})
            await self.qualify()
            return await self.reply(send, 200, {
                "ready": all(self.readiness.values()), "readiness": self.readiness})
        if method != "POST" or path != "/qa/run" or self.backend is None or self.runner is None:
            return await self.reply(send, 404, {"status": "FAIL"})
        if headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json":
            return await self.reply(send, 415, {"status": "FAIL"})
        body = bytearray()
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                return await self.reply(send, 400, {"status": "FAIL"})
            body.extend(event.get("body", b""))
            if len(body) > 2048:
                return await self.reply(send, 413, {"status": "FAIL"})
            if not event.get("more_body"):
                break
        try:
            payload = json.loads(body)
            await self.qualify()
            return await self.reply(
                send, 200, await self.backend.post(payload, self.runner.run, self.readiness))
        except (QAError, ValueError, TypeError, json.JSONDecodeError):
            return await self.reply(send, 400, {"status": "FAIL"})
        finally:
            body[:] = b"\0" * len(body)

    @staticmethod
    async def reply(send, status, value):
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"cache-control", b"no-store"), (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app = QAASGIApp()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18765, access_log=False,
                log_config=None, log_level="critical", workers=1,
                limit_concurrency=1, timeout_keep_alive=1)
