"""Canonical sandbox binds; no service imports or state initialization.

The shell uses the same strict decimal syntax before it can invoke Python.
The public server attribute lookup validates the resolved Config *before*
Uvicorn0.49 enters bind/supervisor/cleanup. The factory and lifespan retain
their independent child checks, including inherited reload/worker sockets.
There is no globally cached public app attribute: each canonical import-string
lookup must validate its own configuration, even after this module is cached.

Supported entry: Uvicorn0.49 CLI or uvicorn.run("sandbox.server:app", ...), with
fresh Config, TCP and lifespan auto/on. Callable/preloaded/custom runners and
private exports are not supported entrypoints. The bounded saved-test lane is
CPython3.13; Desktop CPython3.14 qualification remains separate isolated QA.
Private Uvicorn methods/code objects are a fail-closed compatibility contract;
runtime upgrades require the full outer-run and child controls before use.
"""
from __future__ import annotations

import inspect
import re
import socket
import sys
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


def validate_runtime(uvicorn_version: str, implementation: str, python_version: tuple[int, int]) -> None:
    # Outer load_app ordering and child code-object contracts are specific to
    # this reviewed runtime. Upgrade this policy only with new compatibility QA.
    if uvicorn_version != "0.49.0" or implementation != "cpython" or python_version not in {(3, 13), (3, 14)}:
        raise SandboxBindError("Unsupported sandbox runtime; qualify Uvicorn/Python before use")


def app_lookup_config() -> Any:
    """Resolved Config at the public lookup, before outer bind/finally.

    Uvicorn0.49 run calls Config.load_app before constructing Server and
    entering its try/finally. Config.load repeats the lookup in each child.
    Checking only module import or the later factory would miss cached imports
    or reload/worker parent binds. No installed dependency is modified.
    """
    import uvicorn
    from uvicorn.config import Config

    validate_runtime(uvicorn.__version__, sys.implementation.name, sys.version_info[:2])
    method = getattr(Config, "load_app", None)
    code = getattr(method, "__code__", None)
    if code is None:
        raise SandboxBindError("Unsupported Uvicorn sandbox lookup runtime")
    configs = []
    frame = inspect.currentframe()
    try:
        while frame is not None:
            if frame.f_code is code:
                obj = frame.f_locals.get("self")
                if isinstance(obj, Config):
                    configs.append(obj)
            frame = frame.f_back
        if len(configs) != 1 or configs[0].app != "sandbox.server:app":
            raise SandboxBindError("Missing or ambiguous actual Uvicorn app lookup context")
        validate_config(configs[0])
        return configs[0]
    finally:
        del frame


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
