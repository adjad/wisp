"""Optional local inference boundary. Never imported by offline paths.

The coordinator must supply an independently reviewed outer OS sandbox and a
runtime slot. This module neither starts an engine nor loads/unloads a model.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import sys


def require_isolation(original_home):
    """Require actual home/network denials BEFORE importing service modules."""
    if sys.platform != "darwin" or original_home is None:
        raise ValueError("Live evaluation requires a macOS OS sandbox and --original-home")
    original_home = original_home.resolve(strict=True)
    scratch = Path.home().resolve(strict=True)
    state = Path(os.environ.get("WISP_HOME", "")).resolve(strict=True)
    if scratch == original_home or original_home in scratch.parents or scratch not in state.parents:
        raise ValueError("HOME and WISP_HOME must be isolated outside the real home")
    try:
        fd = os.open(original_home, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EPERM}:
            raise ValueError("Real-home denial control is inconclusive") from None
    else:
        os.close(fd)
        raise ValueError("OS sandbox does not deny opening the real home")
    # Numeric documentation address avoids DNS and has no legitimate test target.
    # A timeout/refusal is NOT evidence of OS denial.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("192.0.2.1", 9))
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM}:
                raise ValueError("External-network denial control is inconclusive") from None
        else:
            raise ValueError("External network was not denied")
    return original_home


class LocalTransport:
    def __init__(self, client, model):
        self.client, self.model = client, model

    async def __call__(self, **kwargs):
        if kwargs["model"] != self.model or self.model not in await self.client.loaded_models():
            raise RuntimeError("Selected model is no longer resident; no load/fallback permitted")
        return await self.client.chat(**kwargs)

    async def aclose(self):
        await self.client.aclose()


async def open_transport(args):
    original = require_isolation(args.original_home)
    # Service imports happen only after physical OS isolation controls pass.
    import service.config as config
    config.OMLX_SETTINGS = original / ".omlx/settings.json"
    config.OMLX_MODEL_SETTINGS = original / ".omlx/model_settings.json"
    from service.config.endpoints import endpoint, is_loopback
    from service.inference.omlx_client import OMLXClient
    ep = endpoint("local")
    if not ep.managed or ep.name != "local" or ep.provider != "omlx" or not is_loopback(ep.base_url):
        raise ValueError("Only the existing managed local oMLX endpoint is permitted")
    client = OMLXClient(timeout=args.timeout)
    try:
        if not client.managed or client.base_url != ep.base_url.rstrip("/") or client.provider.name != "omlx":
            raise ValueError("Inference client does not match the approved local endpoint")
        if args.model not in await client.loaded_models():
            raise ValueError("Selected Ling is not already resident; no model was loaded")
    except BaseException:
        await client.aclose()
        raise
    return LocalTransport(client, args.model)
