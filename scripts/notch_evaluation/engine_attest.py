"""Attest the desktop oMLX engine once, before a contained Notch evaluation run.

Runs in the unconfined supervisor, never in the sandboxed backend: the
production attribution (``RuntimeAuthority`` -> ``DesktopOmlx``) needs lsof
and the setuid ps, which no sandbox may run. It is used exactly as it ships:
the listener on 127.0.0.1:8000 must be the signed ``omlx-server`` child of the
fixed oMLX application. The result is a pin (pid, start time, uid,
executable, parent, signature, served model) that the backend's evaluation
transport re-checks on every request with kernel reads only
(``scripts/notch_evaluation/pinned_engine.py``).

The production attribution looks for ~/.moe/omlx-runtime-authorization.json
only to confirm it is absent (a present one means a managed runtime, which is
refused here). Nothing under ~/.moe is read otherwise or written.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_HOST, ENGINE_PORT = "127.0.0.1", 8000
ENGINE_URL = f"http://{ENGINE_HOST}:{ENGINE_PORT}"
PIN_FIELDS = ("pid", "uid", "start_seconds", "start_microseconds", "executable", "parent_pid",
              "signing_identifier", "team_id", "port", "model", "attested_at", "attribution")


class EngineAttestationError(RuntimeError):
    """The engine on 127.0.0.1:8000 is not one this run may use."""


def loaded_models(*, timeout: float = 5.0) -> list[str]:
    """Ids the engine reports loaded. A read; never loads anything."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{ENGINE_URL}/v1/models/status", timeout=timeout) as response:
        rows = json.loads(response.read()).get("models")
    if not isinstance(rows, list):
        raise EngineAttestationError("the engine's model status is unreadable")
    return sorted(r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
                  and r.get("loaded") is True)


def attest(model: str) -> dict:
    """The production attribution of 127.0.0.1:8000, pinned. Raises on any doubt."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from service.inference.attributed_transport import DesktopOmlx, RuntimeAuthority
    from service.inference.local_peer import AuthRefused, process_bsdinfo, process_identity, process_path
    try:
        authority = RuntimeAuthority().load()
        if not isinstance(authority, DesktopOmlx):
            raise EngineAttestationError("a managed runtime manifest is installed; only the desktop oMLX is pinned")
        pid = authority.binding()
        identity = process_identity(pid, authority.uid)
        executable = str(process_path(pid))
        parent_pid = process_bsdinfo(pid)[1]
        signing, team = authority._csops_string(pid, 11), authority._csops_string(pid, 14)
    except AuthRefused as exc:
        raise EngineAttestationError(f"the production attribution refused the engine: {exc}") from None
    served = loaded_models()
    if model not in served:
        raise EngineAttestationError(f"{model!r} is not loaded on the engine")
    # The same listener and incarnation after the model read: nothing changed underneath.
    try:
        if authority.binding() != pid or process_identity(pid, authority.uid) != identity:
            raise EngineAttestationError("the engine changed during attestation")
    except AuthRefused as exc:
        raise EngineAttestationError(f"the engine changed during attestation: {exc}") from None
    return {"pid": pid, "uid": identity[1], "start_seconds": identity[2], "start_microseconds": identity[3],
            "executable": executable, "parent_pid": parent_pid, "signing_identifier": signing,
            "team_id": team, "port": ENGINE_PORT, "model": model, "attested_at": time.time(),
            "attribution": "DesktopOmlx"}
