"""Consume the native launch bridge before tools can inherit its environment.

Only these fixed references use the private store. Unrelated legacy env:
references retain their existing behavior. Never include values in exceptions.
"""
from __future__ import annotations

import os
import re

from . import quarantine

_NAMES = ("WISP_LOCAL_OMLX_KEY", "WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY")
from service.credential_pipe import consume
_native_frame = 'WISP_CREDENTIAL_PIPE' in os.environ
_VALUES, _generation = consume('primary', optional=True)
if _native_frame and quarantine._gate.expected != _generation:
    _VALUES.clear()
    raise ValueError('Native credential pipe unavailable')
quarantine._gate.callbacks.append(_VALUES.clear)


def resolve(name: str) -> str:
    try:
        quarantine.check()
    except quarantine.CredentialQuarantined:
        _VALUES.clear()
        raise
    if name not in _NAMES:
        return os.environ.get(name, "").strip()
    value = _VALUES.get(name, "")
    if value and not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid native credential bridge value")
    return value


def reviewed_bindings():
    """Receipt from explicit provisioning, separate from editable model config.

    Same-UID compromise is outside this boundary; receipt access is owner-only.
    Missing or legacy provisioning never authorizes sending native mini keys.
    """
    import json
    from pathlib import Path
    import stat
    path = Path.home() / ".moe/provisioning/endpoints.json"
    try:
        for parent in (path.parent, path.parent.parent):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
                raise ValueError
            record = json.load(source)
        if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1 or not re.fullmatch(r"n[A-Za-z0-9]+", record.get("node_id", "")):
            raise ValueError
        return record
    except (OSError, ValueError, TypeError):
        raise ValueError("Reviewed credential binding unavailable") from None


def verify_binding(name, url, *, purpose, node_id=None):
    if name not in ("WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY"):
        return
    expected = "node" if name == "WISP_MINI_NODE_KEY" else "inference"
    if purpose != expected:
        raise ValueError("Credential role mismatch")
    record = reviewed_bindings()
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    host = record.get("host", "")
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net", host):
        raise ValueError("Reviewed credential identity invalid")
    origin = "https://" + host + (":8443" if purpose == "node" else "")
    if url != origin or parsed.username or parsed.password or (purpose == "node" and node_id != record["node_id"]):
        raise ValueError("Credential origin or node identity mismatch")
