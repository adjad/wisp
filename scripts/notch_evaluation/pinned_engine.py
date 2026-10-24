"""The evaluation backend's engine authority: the supervisor's pin, re-checked.

Importable only under the latched evaluation boundary. The sandboxed backend
may run no process, so the production attribution (lsof and the setuid ps)
cannot run there; the supervisor ran it before launch
(``engine_attest.py``) and pinned the result. This authority stands in for
``RuntimeAuthority`` inside the unchanged ``CheckedBackend``/``CheckedStream``:
it connects only to 127.0.0.1:8000 and, before every request write, checks with
kernel reads only that

- the pinned pid is the same incarnation (start time, uid) running the same
  executable (``process_identity``, ``process_path``), and
- this connection's far end is a socket held by that pid
  (``native_connection_owner``: proc_pidinfo/proc_pidfdinfo).

Any mismatch writes a marker in the owned root (the driver stops the run as
ENGINE_NOT_READY) and refuses the request. There is no fallback.

Known gap, stated in the README: per-connection attribution here uses this
pinned identity, not the production lsof/ps path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from service import evaluation_boundary

if not evaluation_boundary.active():
    raise ImportError("pinned_engine is evaluation-only: the evaluation boundary is not latched")

from service.inference.local_peer import (AuthRefused, NativeAnomaly, native_connection_owner,  # noqa: E402
                                          process_identity, process_path, socket_identity)

ENGINE_PORT = 8000
LOST_MARKER = "engine-pin-lost.json"
_FIELDS = {"pid": int, "uid": int, "start_seconds": int, "start_microseconds": int, "executable": str,
           "port": int, "model": str}


def pin_problem(pin) -> str | None:
    """Why a pin from the host config is unusable, or None."""
    if not isinstance(pin, dict):
        return "engine_pin must be an object"
    for name, kind in _FIELDS.items():
        value = pin.get(name)
        if type(value) is not kind:
            return f"engine_pin.{name} must be {kind.__name__}"
    if pin["port"] != ENGINE_PORT or not 0 < pin["pid"] < 2 ** 31 or not pin["start_seconds"]:
        return "engine_pin names another port or an impossible process"
    if not os.path.isabs(pin["executable"]) or pin["uid"] != os.getuid():
        return "engine_pin names a relative executable or another user's process"
    return None


class PinnedAuthority:
    """``load``/``binding``/``connected_peer``, as CheckedBackend uses an authority."""

    port = ENGINE_PORT

    def __init__(self, pin: dict, owned_root: str) -> None:
        problem = pin_problem(pin)
        if problem:
            raise ValueError(problem)
        self.pin, self.uid = dict(pin), pin["uid"]
        self.incarnation = (pin["pid"], pin["uid"], pin["start_seconds"], pin["start_microseconds"])
        self.marker = Path(owned_root) / "evidence" / LOST_MARKER

    def _lost(self, reason: str):
        """Sticky: record why, once, and refuse."""
        evaluation_boundary._record("blocked", "engine", {"reason": reason})
        try:
            fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"reason": reason, "pin": self.pin}, handle)
        return AuthRefused("pinned_engine_lost")

    def _check_process(self) -> None:
        if self.marker.exists():
            raise AuthRefused("pinned_engine_lost")
        pid = self.pin["pid"]
        try:
            if process_identity(pid, self.uid) != self.incarnation:
                raise self._lost("incarnation changed")
            if str(process_path(pid)) != self.pin["executable"]:
                raise self._lost("executable changed")
        except AuthRefused as exc:
            if str(exc) == "pinned_engine_lost":
                raise
            raise self._lost(f"process unreadable: {exc}") from None

    def load(self):
        return self

    def binding(self, expected_pid=None) -> int:
        if expected_pid is not None and expected_pid != self.pin["pid"]:
            raise self._lost("another listener pid")
        self._check_process()
        return self.pin["pid"]

    def connected_peer(self, sock, pid, incarnation):
        if pid != self.pin["pid"] or tuple(incarnation) != self.incarnation:
            raise self._lost("another peer process")
        self._check_process()
        try:
            fd, local, remote = socket_identity(sock, ENGINE_PORT)
            server_fd, server_so, _gen, _client_so, _client_gen = native_connection_owner(pid, fd, local, remote)
        except AuthRefused as exc:
            raise self._lost(f"connection unqualified: {exc}") from None
        except NativeAnomaly as exc:
            raise self._lost(f"connection not held by the pinned engine: {exc}") from None
        self._check_process()
        return (pid, server_fd, server_so)


def install(pin: dict, owned_root: str) -> PinnedAuthority:
    """Every checked engine transport in this process vouches with the pin."""
    from service.inference import attributed_transport
    authority = PinnedAuthority(pin, owned_root)

    def authority_for(port):
        if port != ENGINE_PORT:
            return attributed_transport._UnavailableAuthority()
        return authority

    attributed_transport.authority_for = authority_for
    return authority
