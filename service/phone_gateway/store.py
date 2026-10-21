"""On-disk state for the gateway: the state directory and the paired-device list.

Everything here is private to the user: directory 0700, files 0600, and every
write goes through a temp file plus os.replace so a crash never leaves a half
written devices.json. Only the SHA-256 of a device token is ever stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StoreError(Exception):
    """A state-directory problem the CLI should report plainly."""


def state_dir(create: bool = True) -> Path:
    base = os.environ.get("MOE_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".moe"
    path = root / "phone_gateway"
    if create:
        ensure_private_dir(path)
    return path


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically with mode 0600."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """32 random bytes, base64url without padding (43 characters)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


MAX_NAME_LEN = 64
LAST_SEEN_INTERVAL_S = 60.0


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME_LEN or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise StoreError(f"device name must be 1..{MAX_NAME_LEN} printable characters")
    return name


class DeviceStore:
    """devices.json: a list of {id, name, token_sha256, created_at, last_seen}.

    The CLI and the running server share the file, so reads re-check the file's
    stat signature and reload when another process changed it (a revoke takes
    effect on the next request without restarting the server).
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "devices.json"
        self._lock = threading.Lock()
        self._sig: tuple[int, int] | None = None
        self._cache: list[dict[str, Any]] = []
        self._touched: dict[str, float] = {}

    # -- reading ----------------------------------------------------------
    def _read_file(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise StoreError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise StoreError(f"{self.path} is not a JSON list")
        return [item for item in raw
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and isinstance(item.get("token_sha256"), str)]

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                st = self.path.stat()
                sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                sig = None
            if sig is None or sig != self._sig:
                try:
                    self._cache = self._read_file()
                except StoreError:
                    # A corrupt file fails closed: nobody authenticates.
                    self._cache = []
                self._sig = sig
            return [dict(d) for d in self._cache]

    def match(self, token_hash: str) -> dict[str, Any] | None:
        """Constant-time compare against every stored hash (no early exit)."""
        found = None
        probe = token_hash.encode("ascii")
        for device in self.devices():
            stored = str(device.get("token_sha256", "")).encode("utf-8", "replace")
            if hmac.compare_digest(probe, stored) and found is None:
                found = device
        return found

    # -- writing ----------------------------------------------------------
    def _save(self, devices: list[dict[str, Any]]) -> None:
        ensure_private_dir(self.directory)
        atomic_write_json(self.path, devices)
        self._sig = None  # force a reload on the next read

    def add(self, name: str, token: str) -> dict[str, Any]:
        name = clean_name(name)
        with self._lock:
            devices = self._read_file()
            used = {d["id"] for d in devices}
            device_id = secrets.token_hex(4)
            while device_id in used:
                device_id = secrets.token_hex(4)
            device = {"id": device_id, "name": name, "token_sha256": hash_token(token),
                      "created_at": now_iso(), "last_seen": None}
            devices.append(device)
            self._save(devices)
            return dict(device)

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            devices = self._read_file()
            kept = [d for d in devices if d["id"] != device_id]
            if len(kept) == len(devices):
                return False
            self._save(kept)
            return True

    def due_for_touch(self, device_id: str, clock=time.monotonic) -> bool:
        with self._lock:
            last = self._touched.get(device_id)
            return last is None or clock() - last >= LAST_SEEN_INTERVAL_S

    def touch(self, device_id: str, clock=time.monotonic) -> bool:
        """Record last_seen, at most once a minute per device. True if written."""
        now = clock()
        with self._lock:
            last = self._touched.get(device_id)
            if last is not None and now - last < LAST_SEEN_INTERVAL_S:
                return False
            self._touched[device_id] = now
            devices = self._read_file()  # fresh read: never resurrect a revoked device
            for device in devices:
                if device["id"] == device_id:
                    device["last_seen"] = now_iso()
                    self._save(devices)
                    return True
            return False
