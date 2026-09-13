"""Versioned admission contract. No model loading, eviction or hardware guesses.

Telemetry is a private supervisor-owned attestation, not an oMLX API response.
The supervisor must independently enforce the reported engine limits. Python
admission/cancellation supplements those limits; it cannot cap another process.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import threading

GB = 1_000_000_000
MAX_INT = 2**63 - 1
POLICY = dict(schema_version=1, memory_guard_bytes=60*GB, hot_cache_bytes=2*GB,
              paged_kv_bytes=20*GB, minimum_free_bytes=150*GB, reserve_bytes=50*GB,
              concurrency=1, expert_offload=False, minimum_quantization_bits=4)


class ResourceRefusal(RuntimeError):
    """Fixed, public reason codes; never attach paths or telemetry contents."""


def integer(value, low=0, high=MAX_INT):
    if type(value) is not int or not low <= value <= high:
        raise ResourceRefusal("resource_malformed")
    return value


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ResourceRefusal("resource_malformed")
            result[key] = value
        return result
    def invalid(_):
        raise ResourceRefusal("resource_malformed")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError, RecursionError):
        raise ResourceRefusal("resource_malformed") from None


def private_read(path, maximum=65536):
    """Bounded no-follow read with identity checks on the opened file."""
    path = Path(path)
    try:
        if not path.is_absolute() or any(p.is_symlink() for p in path.parents):
            raise ResourceRefusal("resource_unavailable")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or before.st_mode & 0o077 or before.st_size > maximum):
                raise ResourceRefusal("resource_unavailable")
            data = source.read(maximum + 1)
            after = os.fstat(source.fileno())
            if (len(data) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ResourceRefusal("resource_unavailable")
            return data
    except OSError:
        raise ResourceRefusal("resource_unavailable") from None


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def contract(raw):
    if not isinstance(raw, dict) or set(raw) != set(POLICY) | {"models"}:
        raise ResourceRefusal("resource_malformed")
    for key, value in POLICY.items():
        if type(raw[key]) is not type(value) or raw[key] != value:
            raise ResourceRefusal("resource_policy_mismatch")
    if not isinstance(raw["models"], list) or not 1 <= len(raw["models"]) <= 8:
        raise ResourceRefusal("model_unqualified")
    seen = set()
    fields = {"model_id", "revision", "tokenizer_revision", "runtime_revision", "profile_sha256",
              "quantization_bits", "context_tokens", "qualified_contexts", "request_memory_bytes",
              "request_paged_bytes"}
    for model in raw["models"]:
        if not isinstance(model, dict) or set(model) != fields:
            raise ResourceRefusal("resource_malformed")
        mid = model["model_id"]
        if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", mid) or mid in seen:
            raise ResourceRefusal("model_identity_mismatch")
        seen.add(mid)
        for field in ("revision", "tokenizer_revision", "runtime_revision"):
            if not isinstance(model[field], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", model[field]):
                raise ResourceRefusal("model_revision_required")
        if not isinstance(model["profile_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", model["profile_sha256"]):
            raise ResourceRefusal("model_revision_required")
        integer(model["quantization_bits"], 4, 32)
        integer(model["context_tokens"], 8192, 16384)
        contexts = model["qualified_contexts"]
        if (not isinstance(contexts, list) or any(type(n) is not int for n in contexts)
                or contexts not in ([8192], [8192, 16384]) or model["context_tokens"] != contexts[-1]):
            raise ResourceRefusal("model_qualification_progression")
        integer(model["request_memory_bytes"], 1, 60*GB)
        integer(model["request_paged_bytes"], 0, 20*GB)
    # Own a deep copy so caller mutations cannot weaken checked policy.
    return strict_json(canonical(raw))


@contextmanager
def volume_lease(path, *, timeout=0):
    """Serialize owned growth on one device, across stores and processes.

    Lock identity is fixed by uid/device, independent of runtime configuration
    or destination directory. Never unlink a lock: replacement would split it.
    Noncooperating processes require the independently qualified OS quota.
    """
    from mini.store import private_directory
    root = private_directory(Path("/tmp").resolve() / f"wisp-mini-volume-{os.getuid()}")
    fd = None
    try:
        device = Path(path).stat().st_dev
        lock = root / f"{device}.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ResourceRefusal("volume_lease_unavailable")
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise ResourceRefusal("volume_busy") from None
                time.sleep(0.005)
        yield
    except OSError:
        raise ResourceRefusal("volume_lease_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


class Unqualified:
    def check(self, **kwargs):
        raise ResourceRefusal("resource_unavailable")

    @contextmanager
    def admission(self, request):
        self.check()
        yield  # pragma: no cover


class ResourceGuard:
    def __init__(self, configuration, sample, free_bytes, *, clock=time.monotonic_ns, lock_path=None, storage_path=None):
        self.configuration = contract(configuration)
        self.digest = hashlib.sha256(canonical(self.configuration)).hexdigest()
        self.sample, self.free_bytes, self.clock = sample, free_bytes, clock
        self.lock_path = lock_path
        self.storage_path = storage_path
        self._admission = threading.Lock()

    @classmethod
    def from_files(cls, configuration, telemetry):
        configuration, telemetry = Path(configuration), Path(telemetry)
        from mini.store import private_directory
        parent = private_directory(telemetry.parent)
        def free():
            fs = os.statvfs(parent)
            return fs.f_bavail * fs.f_frsize
        return cls(strict_json(private_read(configuration)),
                   lambda: strict_json(private_read(telemetry)), free,
                   lock_path=parent / "inference.lock", storage_path=parent)

    def check(self, *, preflight=False, model=None):
        try:
            data = self.sample()
            fields = {"schema_version", "contract_sha256", "sampled_at_ns", "memory_used_bytes",
                      "hot_cache_used_bytes", "paged_kv_used_bytes", "engine_limits_enforced"}
            if not isinstance(data, dict) or set(data) != fields:
                raise ResourceRefusal("resource_malformed")
            if type(data["schema_version"]) is not int or data["schema_version"] != 1:
                raise ResourceRefusal("resource_malformed")
            if data["contract_sha256"] != self.digest or data["engine_limits_enforced"] is not True:
                raise ResourceRefusal("resource_attestation_mismatch")
            age = integer(self.clock()) - integer(data["sampled_at_ns"])
            if not 0 <= age <= 2_000_000_000:
                raise ResourceRefusal("resource_telemetry_stale")
            used = integer(data["memory_used_bytes"])
            hot = integer(data["hot_cache_used_bytes"])
            paged = integer(data["paged_kv_used_bytes"])
            free = integer(self.free_bytes())
            extra_memory = model["request_memory_bytes"] if model else 0
            extra_paged = model["request_paged_bytes"] if model else 0
            if used > 60*GB - extra_memory or hot > 2*GB or paged > 20*GB - extra_paged:
                raise ResourceRefusal("resource_capacity_exceeded")
            if free < (150*GB if preflight else 50*GB) + extra_paged:
                raise ResourceRefusal("storage_capacity_refused")
        except ResourceRefusal:
            raise
        except Exception:
            raise ResourceRefusal("resource_unavailable") from None

    def request_model(self, request):
        model = next((m for m in self.configuration["models"] if m["model_id"] == request.get("model")), None)
        if model is None:
            raise ResourceRefusal("model_unqualified")
        # UTF-8 bytes plus template overhead is a conservative text-token upper
        # bound for the qualified tokenizer. Qualification must prove this bound.
        tokens = len(canonical({k: v for k, v in request.items() if k != "max_tokens"})) + 256
        tokens += integer(request.get("max_tokens", 0), 0, 16384)
        if tokens > model["context_tokens"]:
            raise ResourceRefusal("model_context_exceeded")
        return model

    @contextmanager
    def admission(self, request):
        if not self._admission.acquire(blocking=False):
            raise ResourceRefusal("resource_busy")
        fd = None
        volume = None
        try:
            if self.lock_path is not None:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise ResourceRefusal("resource_unavailable")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ResourceRefusal("resource_busy") from None
            if self.storage_path is not None:
                lease = volume_lease(self.storage_path)
                lease.__enter__()
                volume = lease
            model = self.request_model(request)
            self.check(model=model)
            yield
        except OSError:
            raise ResourceRefusal("resource_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)
            if volume is not None:
                volume.__exit__(None, None, None)
            self._admission.release()
