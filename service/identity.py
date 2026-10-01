"""Who this backend is, so the app can refuse to act on one that is not Wisp's own.

The app's backend port (8765) is hard-coded, and the app used to trust whatever
answered there: it ran every "send this email / create this event" request that arrived
over that port against the user's real Mail, Messages and Calendar. A sandbox backend
started on 8765 while the real app was open, or any other program that took the port,
could therefore make the real app act.

Two things make that refusable. The app proves the LISTENER is its own backend from
the kernel's record of the executable (that part does not trust this module at all),
and it asks this endpoint which MODE the backend is in, so a backend that is Wisp's
code but running against a sandbox world is refused too. A sandbox backend also
refuses to start on a production port, as defence in depth.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from service.paths import MOE_DIR

# Ports the real app and its engine own. Nothing sandboxed may bind or proxy to them.
PRODUCTION_BACKEND_PORT = 8765
PRODUCTION_PORTS = frozenset({PRODUCTION_BACKEND_PORT, 8000})


def is_sandbox() -> bool:
    """True for any backend that is not running against the user's real ~/.moe."""
    if os.environ.get("WISP_SANDBOX_HOME"):
        return True
    try:
        return MOE_DIR.resolve() != (Path.home() / ".moe").resolve()
    except OSError:
        return True  # cannot tell: never claim to be the real one


def mode() -> str:
    return "sandbox" if is_sandbox() else "production"


def payload() -> dict:
    return {"service": "wisp-backend", "mode": mode(), "pid": os.getpid()}


def bind_port(argv: list[str] | None = None) -> int | None:
    """The port this process was told to listen on (uvicorn's --port), if any."""
    args = list(sys.argv if argv is None else argv)
    for index, arg in enumerate(args):
        value = None
        if arg == "--port" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--port="):
            value = arg.partition("=")[2]
        if value is not None:
            return int(value) if re.fullmatch(r"[0-9]{1,5}", value) else None
    return None


def refuse_sandbox_on_production_port(argv: list[str] | None = None,
                                      *, sandbox: bool | None = None) -> None:
    """A sandbox backend must never listen where the real app looks for its backend."""
    port = bind_port(argv)
    if (is_sandbox() if sandbox is None else sandbox) and port in PRODUCTION_PORTS:
        raise SystemExit(
            f"Refusing to start: this is a sandbox backend (not the real ~/.moe) and "
            f"port {port} belongs to the real Wisp. Use another port, for example 8775.")
