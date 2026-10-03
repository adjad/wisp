"""Canonical sandbox binds; no service imports or state initialization.

The shell uses the same strict decimal syntax before it can invoke Python.
The server factory reads Uvicorn's *resolved* Config in its real load/serve
callchain, not a guessed argv/default or a SANDBOX_PORT claim. It also checks
inherited reload/worker sockets. Unsupported/preloaded/custom runners fail
closed; update the guard and its offline controls when changing server APIs.
"""
from __future__ import annotations

import inspect
import re
import socket
from dataclasses import dataclass
from types import FrameType
from typing import Any

PRODUCTION_PORTS = frozenset({8000, 8765})


class SandboxBindError(RuntimeError):
    pass


def canonical_port(value: str | int, *, what: str) -> int:
    if type(value) is int:
        port = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,4}", value):
        port = int(value)
    else:
        raise SandboxBindError(f"{what} must be a canonical decimal port in 1..65535")
    if not 1 <= port <= 65535:
        raise SandboxBindError(f"{what} must be a canonical decimal port in 1..65535")
    if port in PRODUCTION_PORTS:
        raise SandboxBindError(f"{what}: port {port} belongs to the real Wisp")
    return port


def validate_config(config: Any) -> int:
    if config.uds is not None or config.fd is not None:
        raise SandboxBindError("Cannot establish a safe TCP bind from --uds / --fd")
    if config.lifespan not in {"on", "auto"}:
        raise SandboxBindError("Sandbox startup requires lifespan validation")
    return canonical_port(config.port, what="sandbox server bind")


@dataclass(frozen=True)
class StartupBind:
    config: Any
    server_frame: FrameType

    def check(self) -> int:
        port = validate_config(self.config)
        runner = self.server_frame.f_locals.get("self")
        if runner is None or runner.config is not self.config:
            raise SandboxBindError("Sandbox bind context changed before startup")
        sockets = self.server_frame.f_locals.get("sockets")
        if sockets is not None:
            if not isinstance(sockets, list) or not sockets:
                raise SandboxBindError("Missing or ambiguous inherited sandbox listeners")
            for listener in sockets:
                try:
                    address = listener.getsockname()
                    family = listener.family
                except (AttributeError, OSError) as error:
                    raise SandboxBindError("Cannot establish inherited sandbox bind") from error
                if family not in {socket.AF_INET, socket.AF_INET6} or not isinstance(address, tuple) or len(address) not in {2, 4}:
                    raise SandboxBindError("Cannot establish inherited sandbox TCP bind")
                actual = canonical_port(address[1], what="inherited sandbox listener")
                if actual != port:
                    raise SandboxBindError("Configured and inherited sandbox bind ports disagree")
        return port


def startup_bind() -> StartupBind:
    from uvicorn.config import Config
    from uvicorn.server import Server

    configs = []
    runners = []
    frame = inspect.currentframe()
    try:
        while frame is not None:
            if frame.f_code is Config.load.__code__:
                obj = frame.f_locals.get("self")
                if isinstance(obj, Config):
                    configs.append(obj)
            if frame.f_code is Server._serve.__code__:
                obj = frame.f_locals.get("self")
                if isinstance(obj, Server):
                    runners.append(frame)
            frame = frame.f_back
        if len(configs) != 1 or len(runners) != 1:
            raise SandboxBindError("Missing or ambiguous actual Uvicorn sandbox bind context")
        result = StartupBind(configs[0], runners[0])
        result.check()
        return result
    finally:
        del frame


def lifespan_bind() -> Any:
    from uvicorn.lifespan.on import LifespanOn

    configs = []
    frame = inspect.currentframe()
    try:
        while frame is not None:
            if frame.f_code is LifespanOn.main.__code__:
                obj = frame.f_locals.get("self")
                if isinstance(obj, LifespanOn):
                    configs.append(obj.config)
            frame = frame.f_back
        if len(configs) != 1:
            raise SandboxBindError("Missing or ambiguous sandbox lifespan bind context")
        validate_config(configs[0])
        return configs[0]
    finally:
        del frame
