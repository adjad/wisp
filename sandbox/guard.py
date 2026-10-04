"""The sandbox must never touch the real Wisp's ports.

Two ways it could, both reachable by one environment variable:
  * a sandbox BACKEND on 8765 gets the real app's connections, and the real app runs
    whatever "send this email" requests it sends, against real accounts;
  * the sandbox server pointed at the REAL backend acts as a second app on its event
    stream: it claims real outbound sends and acknowledges them without sending.
The launcher only documented the prohibition. This enforces it, for every spelling a
URL or port can take.
"""
from __future__ import annotations

import ipaddress
import re
import sys
from urllib.parse import urlsplit

from service.identity import PRODUCTION_PORTS


class SandboxIsolationError(RuntimeError):
    pass


def port_of(value: str | int | None) -> int | None:
    """The TCP port a URL, host:port or bare port refers to (default ports included)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if re.fullmatch(r"[0-9]{1,5}", text):
        return int(text)
    parsed = urlsplit(text if "://" in text else "//" + text)
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(parsed.scheme)


def refuse_production(value: str | int | None, *, what: str) -> None:
    """Raise unless `value` points somewhere other than a production port."""
    port = port_of(value)
    if value is not None and str(value).strip() and port is None:
        raise SandboxIsolationError(f"{what} {value!r} is not a recognisable URL or port.")
    if port in PRODUCTION_PORTS:
        raise SandboxIsolationError(
            f"{what} points at port {port}, which belongs to the real Wisp. The sandbox "
            "never uses it (default is 8775 for its backend, 8766 for its server).")


if __name__ == "__main__":  # used by sandbox/run.sh: python -m sandbox.guard <port> [<port>...]
    try:
        for index, arg in enumerate(sys.argv[1:], start=1):
            refuse_production(arg, what=f"argument {index}")
    except SandboxIsolationError as error:
        print(f"refusing to run the sandbox: {error}", file=sys.stderr)
        raise SystemExit(1)
