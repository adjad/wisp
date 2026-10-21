"""Resolve this Mac's Tailscale identity and decide where the gateway may bind."""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess

LOOPBACK_OVERRIDES = ("127.0.0.1", "::1")
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_CANDIDATES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


class BindError(Exception):
    """The gateway refuses to start; the message says why."""


def find_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _run(args: list[str], timeout: float = 8.0) -> str | None:
    binary = find_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def tailscale_ipv4() -> str | None:
    """The first valid Tailscale IPv4 from `tailscale ip -4`, or None."""
    out = _run(["ip", "-4"])
    if not out:
        return None
    for line in out.splitlines():
        try:
            addr = ipaddress.IPv4Address(line.strip())
        except ValueError:
            continue
        if addr in _TAILSCALE_V4:
            return str(addr)
    return None


def magicdns_name() -> str | None:
    """`Self.DNSName` from `tailscale status --json`, trailing dot removed."""
    out = _run(["status", "--json"])
    if not out:
        return None
    try:
        name = json.loads(out).get("Self", {}).get("DNSName")
    except (ValueError, AttributeError):
        return None
    if isinstance(name, str):
        name = name.strip().rstrip(".")
        return name or None
    return None


def resolve_bind_host(override: str | None) -> str:
    """The address `serve` binds. Never 0.0.0.0, `::` or a LAN address."""
    if override is not None:
        if override not in LOOPBACK_OVERRIDES:
            raise BindError(
                f"--host {override!r} refused: only 127.0.0.1 or ::1 may be forced "
                "(for tests/debugging). Without --host the gateway binds the Tailscale address.")
        return override
    ip = tailscale_ipv4()
    if ip is None:
        raise BindError(
            "no Tailscale IPv4 address (`tailscale ip -4` returned nothing). Start Tailscale "
            "and sign in; the gateway never binds anything else.")
    return ip
