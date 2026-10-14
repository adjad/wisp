"""Attribute Wisp's own bundled oMLX engine before credentials or prompts reach it.

Design: docs/plans/2026-10-13-bundled-omlx-attribution-design.md. In short, the engine
is accepted only if it is the exact process this backend launched (pid, uid and start
time recorded at spawn), is this backend's direct child, is the only listener on the
private loopback port, runs the expected interpreter, and its code trees pass the
same permission audit as the desktop oMLX. Signature strength follows the backend's
own signature; there is no flag that weakens it.

Imported lazily and only for the bundled port, so the mini bundle and staged QA
packages (which ship attributed_transport without this module) refuse instead.
"""
from __future__ import annotations

import logging
import os
import re
import struct
from pathlib import Path
from types import SimpleNamespace

from .attributed_transport import DesktopOmlx, _Inspections, inspect_command
from .local_peer import AuthRefused, process_identity, tcp_listeners

BUNDLED_PORT = 8791        # asserted equal to service.engine.BUNDLED_PORT in the tests
ENGINE_SIGNING_ID = "com.wisp.engine.python3"   # set by the packaging step when signing py/bin/python3
_CS_VALID = 0x00000001
_CS_RUNTIME = 0x00010000
_CS_VALID_RUNTIME = _CS_VALID | _CS_RUNTIME
_log = logging.getLogger("wisp.attestation")


def _default_source():
    from service.engine.runtime import manager
    return manager().incarnation()


def _enclosing_app(root: Path) -> Path:
    """The .app bundle holding the engine, else the engine root (source checkout)."""
    for parent in (root, *root.parents):
        if parent.suffix == ".app":
            return parent
    return root


class BundledOmlx(DesktopOmlx):
    """The engine Wisp launched, qualified like DesktopOmlx but pinned to our own child."""
    port = BUNDLED_PORT
    signing_id = ENGINE_SIGNING_ID

    def __init__(self, source=None):
        # DesktopOmlx.__init__ is not called: its state is oMLX.app-specific.
        self.uid = os.getuid()
        self._group_cache = {}
        self._qualified_roots = set()
        self.prep = SimpleNamespace(run=inspect_command)
        self._source = source or _default_source
        inc = self._expected()
        self.engine_root = inc.root
        self.python_root = self._python_root(inc.root)
        self.site_root = inc.root / "sp"
        self.code_root = inc.root / "omlx"
        self.app_root = _enclosing_app(inc.root)
        self.app_executable = inc.exe
        self.server_entry = self.code_root / "server.py"
        self.team_id, self.strength = self._backend_strength()
        self._identity = self._listener()

    # -- inputs ----------------------------------------------------------------
    def _expected(self):
        inc = self._source()
        if inc is None:
            raise AuthRefused("bundled_engine_not_owned")
        return inc

    def _backend_strength(self):
        """('team', 'signed') when this backend is validly signed with hardened runtime,
        ('', 'unsigned-dev') otherwise. Read from the kernel; never from config or env."""
        flags, = struct.unpack("=I", self._csops(os.getpid(), 0, 4))
        if flags & _CS_RUNTIME and not flags & _CS_VALID:
            # Hardened runtime was requested but the signature no longer verifies: a signed
            # backend that lost its valid flag must not fall back to the weaker rules.
            raise AuthRefused("desktop_signature_unqualified")
        if flags & _CS_VALID_RUNTIME != _CS_VALID_RUNTIME:
            return "", "unsigned-dev"
        team = self._csops_string(os.getpid(), 14)   # a signed backend without a Team ID refuses
        return team, "signed"

    @staticmethod
    def _python_root(root: Path) -> Path:
        return root / "py"

    def _tree_roots(self):
        """Every directory whose files the engine can execute or import."""
        return (self.python_root, self.site_root, self.code_root)

    # -- the qualification -------------------------------------------------------
    def _listener(self):
        inc = self._expected()
        if (inc.root != self.engine_root or inc.exe != self.app_executable
                or process_identity(inc.pid, self.uid)
                != (inc.pid, inc.uid, inc.start_seconds, inc.start_microseconds)):
            raise AuthRefused("process_identity_changed")
        with _Inspections() as inspections:
            listen = inspections.run(lambda: inspect_command(
                ["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-Fpufn"]))
            table = inspections.run(lambda: tcp_listeners())
            rows = listen.result().decode("ascii").splitlines()
            if (len(rows) != 4 or rows[:2] != [f"p{inc.pid}", f"u{self.uid}"]
                    or not re.fullmatch(r"f[0-9]+", rows[2])
                    or rows[3] != f"n127.0.0.1:{self.port}"):
                raise AuthRefused("desktop_listener_unqualified")
            listeners = table.result()
            if (listeners is None
                    or [(host, port) for host, port in listeners if port == self.port]
                    != [("127.0.0.1", self.port)]):
                raise AuthRefused("desktop_listener_unqualified")
            server_process = inspections.run(self._process, inc.pid)
            server_lookup = inspections.run(self._executable, inc.pid)
            server_signature = None
            if self.strength == "signed":
                server_signature = inspections.run(self._signed_process, inc.pid, self.signing_id)
            parent_pid, uid, _command = server_process.result()
            if uid != self.uid:
                raise AuthRefused("desktop_process_unqualified")
            if parent_pid != os.getpid():
                raise AuthRefused("desktop_parent_unqualified")
            executable = self._qualified_file(server_lookup.result(), exact=inc.exe,
                                              parent=self.python_root)
            if server_signature is not None:
                server_signature.result()
        for root in self._tree_roots():
            self._qualified_tree(root)
        _log.debug("bundled_attestation strength=%s", self.strength)
        return inc.pid, executable, parent_pid, ""

    def binding(self, expected_pid=None):
        identity = self._listener()
        if (expected_pid is not None and identity[0] != expected_pid) or identity != self._identity:
            raise AuthRefused("desktop_listener_changed")
        return identity[0]


class BundledAuthority:
    """`CheckedBackend`'s view: load() qualifies the engine afresh for each connection."""
    port = BUNDLED_PORT

    def __init__(self, source=None):
        self.source = source

    def load(self):
        return BundledOmlx(self.source)
