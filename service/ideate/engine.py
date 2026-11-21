"""Ideate and the engine: where it is, whether Inkling is there and loaded, and residency (D5).

Wisp keeps one chat model warm for Ask (main.py's keep-warm set; service/memory_saver.py
parks it when Wisp is idle; service/idle_unloader.py drops other idle models). Ideate fits
into that instead of fighting it:

- When a run starts, Ideate holds the engine for Inkling (service/inference/engine_hold.py)
  and always calls the engine client's swap helper, `OMLXClient.ensure_only(model,
  exclusive=True)`, the same call a Research job makes: on Wisp's bundled engine it first
  unloads every other model (Ask's included), waits until they are gone, then loads Inkling,
  so only Inkling is resident. On the user's own oMLX.app it unloads nothing and only loads.
- During the run Inkling is registered as in flight (`idle.begin`), so the idle unloader
  never drops it mid-run, and the run counts as a foreground turn (`idle.begin_foreground`),
  so background model work (mail gists, the Brief, Save memory's batch windows) stands
  down. While the hold is set, loading any other model is refused with a plain message
  (an Ask turn says Ideate is building and to ask again when it finishes): no swap mid-run,
  never two models resident.
- After the run the hold and both registrations are released, and Inkling is unloaded
  RELEASE_AFTER_S later unless another run has started (a follow-up message reuses it).
  Ask's model is not loaded back straight away: the next Ask, Brief or Day turn loads it,
  as it always does. An Ask turn in that grace swaps Inkling out the normal way.

A run is a child process talking to the engine directly, so these calls are made from the
runner's thread onto the service's own event loop (`bind_loop`), where the shared client
state (prewarm task, epochs) lives.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import ipaddress
import logging
import os
import shutil
import uuid
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import urlsplit

from service.ideate import config
from service.ideate.harness import EngineTarget

log = logging.getLogger(__name__)


class EngineUnavailable(RuntimeError):
    pass


@dataclass
class ModelState:
    reachable: bool
    listed: bool
    loaded: bool


def target() -> EngineTarget:
    """The engine Ideate's harness talks to: Wisp's configured oMLX, loopback only."""
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        raise EngineUnavailable("Ideate needs Wisp's on-Mac engine. This Mac uses a cloud model.")
    from service.config import omlx_base_url
    parts = urlsplit(omlx_base_url())
    host = (parts.hostname or "").lower()
    if host == "localhost":
        host = "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "http" or not loopback:
        raise EngineUnavailable("Ideate needs the engine on this Mac.")
    port = parts.port or 80
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return EngineTarget(base_url=f"http://{netloc}/v1", port=port)


def api_key() -> str:
    """The engine's key, for the harness's environment only (never written to a file)."""
    from service.config import omlx_api_key
    try:
        return omlx_api_key() or ""
    except Exception:  # noqa: BLE001 - an engine without a key
        return ""


class Residency:
    """The default engine hooks. Tests pass a stand-in with the same methods."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not loop:
            self._loop = loop
            self._client = None

    def _get_client(self):
        if self._client is None:
            from service.inference.omlx_client import OMLXClient
            self._client = OMLXClient()
        return self._client

    async def state(self) -> ModelState:
        try:
            rows = (await asyncio.wait_for(self._get_client().status(), 5))["models"]
        except Exception:  # noqa: BLE001 - engine down or not oMLX
            return ModelState(False, False, False)
        row = next((r for r in rows if isinstance(r, dict) and r.get("id") == config.MODEL_ID), None)
        return ModelState(True, row is not None, bool(row and row.get("loaded")))

    async def _ensure(self) -> None:
        await self._get_client().ensure_only(config.MODEL_ID, exclusive=True,
                                             settle_timeout=config.LOAD_TIMEOUT_S)

    def _on_loop(self, coro, timeout: float):
        if self._loop is None or self._loop.is_closed():
            coro.close()
            raise EngineUnavailable("Wisp's engine isn't ready yet.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # Called from the runner's thread.
    def state_sync(self) -> ModelState:
        return self._on_loop(self.state(), 15)

    def ensure_sync(self) -> None:
        self._on_loop(self._ensure(), config.LOAD_TIMEOUT_S + 15)

    def begin(self) -> None:
        from service import idle
        from service.inference import engine_hold
        self._cancel_release()
        engine_hold.hold(config.MODEL_ID, "Ideate")
        idle.begin(config.MODEL_ID)
        idle.begin_foreground()

    def end(self) -> None:
        from service import idle
        from service.inference import engine_hold
        idle.end(config.MODEL_ID)
        idle.end_foreground()
        engine_hold.release(config.MODEL_ID)
        self._schedule_release()

    def wait_for_ask(self, timeout: float = config.ASK_WAIT_S, poll: float = 0.5) -> None:
        """Let an Ask turn that is answering right now finish before the run takes the engine."""
        import time
        from service import idle
        deadline = time.monotonic() + timeout
        while idle.foreground_busy() and time.monotonic() < deadline:
            time.sleep(poll)

    # --- Unloading Inkling after a run -----------------------------------------------------
    _release_handle: asyncio.TimerHandle | None = None

    def _schedule_release(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._arm_release)

    def _cancel_release(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._disarm_release)

    def _disarm_release(self) -> None:
        if self._release_handle is not None:
            self._release_handle.cancel()
            self._release_handle = None

    def _arm_release(self) -> None:
        self._disarm_release()
        loop = asyncio.get_running_loop()
        self._release_handle = loop.call_later(
            config.RELEASE_AFTER_S, lambda: loop.create_task(self.release_now()))

    async def release_now(self) -> bool:
        """Unload Inkling if no run holds it. Wisp's bundled engine only (on the user's own
        oMLX, other apps may be using what is loaded). True when an unload was sent."""
        from service import idle
        from service.inference import engine_hold
        self._release_handle = None
        if engine_hold.holder() is not None or idle.in_flight(config.MODEL_ID) > 0:
            return False
        try:
            client = self._get_client()
            if not getattr(client, "managed", False) or not bundled():
                return False
            if config.MODEL_ID not in set(await client.loaded_models()):
                return False
            await client.unload(config.MODEL_ID)
        except Exception as exc:  # noqa: BLE001 - best effort; the idle unloader is the backstop
            log.info("ideate: unloading the model failed: %s", type(exc).__name__)
            return False
        log.info("ideate: unloaded %s after the run", config.MODEL_ID)
        return True


# --- A copy of the model that is already on this Mac ---------------------------------------

class LinkRefused(RuntimeError):
    """The link was not made; the text is plain words for the tab."""


def _home() -> Path:
    return Path.home()


def models_dir() -> Path:
    """The bundled engine's own models folder (the engine scans only this, plus folders added
    in its admin page)."""
    from service.engine import mode
    return mode.engine_data_dir() / "models"


def bundled() -> bool:
    from service.engine import mode
    return mode.is_bundled()


def target_path() -> Path:
    return models_dir() / config.MODEL_ORG / config.MODEL_ID


def _valid_model_folder(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        names = [p.name for p in path.iterdir()]
    except OSError:
        return False
    return "config.json" in names and any(n.endswith(".safetensors") for n in names)


def _search(base: Path, depth: int):
    """Folders called MODEL_ID under base, at most `depth` levels down."""
    if depth < 0 or not base.is_dir() or base.is_symlink():
        return
    try:
        children = sorted(base.iterdir())
    except OSError:
        return
    for child in children:
        if child.name == config.MODEL_ID and child.is_dir() and not child.is_symlink():
            yield child
        elif depth > 0 and child.is_dir() and not child.is_symlink():
            yield from _search(child, depth - 1)


def find_local_copy() -> Path | None:
    """A valid copy of the model outside Wisp's models folder, or None."""
    home = _home()
    own = target_path()
    for rel in config.MODEL_SEARCH_DIRS:
        for found in _search(home / rel, config.MODEL_SEARCH_DEPTH):
            if found != own and _valid_model_folder(found):
                return found
    snapshots = (home / config.HF_CACHE_DIR / f"models--{config.MODEL_ORG}--{config.MODEL_ID}" / "snapshots")
    try:
        revisions = sorted(snapshots.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        revisions = []
    for rev in revisions:
        if _valid_model_folder(rev):
            return rev
    return None


def link_pending() -> bool:
    """Already cloned into the engine's folder (so it appears when the engine next starts)."""
    return _valid_model_folder(target_path())


def _clonefile(src: Path, dst: Path) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _same_volume(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def _plan(source: Path) -> list[tuple[Path, Path]]:
    """Every regular file to clone, as (real file, path relative to the model folder). Symlinks
    are refused, except inside a Hugging Face snapshot, where a link is fine only when it lands
    in that model's own blobs folder."""
    hf_blobs = None
    if source.parent.name == "snapshots" and source.parent.parent.name.startswith("models--"):
        hf_blobs = (source.parent.parent / "blobs").resolve()
    out: list[tuple[Path, Path]] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():
                raise LinkRefused("That copy contains a shortcut, so Wisp won't use it.")
        for name in filenames:
            f = here / name
            real = f
            if f.is_symlink():
                real = f.resolve()
                if hf_blobs is None or hf_blobs not in real.parents:
                    raise LinkRefused("That copy contains a shortcut, so Wisp won't use it.")
            if not real.is_file():
                raise LinkRefused("That copy is incomplete, so Wisp won't use it.")
            out.append((real, f.relative_to(source)))
    return out


def link_model(source: Path | None = None) -> Path:
    """Clone the local copy into the engine's models folder (APFS clonefile: no extra disk,
    no 13 GB copy). Refuses rather than copying when a clone isn't possible. The source is only
    read. The clone is made under a temporary name, then renamed. Returns the final folder."""
    if not bundled():
        raise LinkRefused("Wisp is using your own oMLX. Add the folder in oMLX.")
    source = source or find_local_copy()
    if source is None:
        raise LinkRefused(f"{config.MODEL_LABEL} isn't on this Mac.")
    if source.is_symlink() or not _valid_model_folder(source):
        raise LinkRefused("That copy is incomplete or is a shortcut, so Wisp won't use it.")
    final = target_path()
    if final.exists() or final.is_symlink():
        raise LinkRefused("Wisp's model folder already has it. It shows up when Wisp's engine restarts.")
    parent = final.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or models_dir().is_symlink():
        raise LinkRefused("Wisp's model folder is a shortcut, so nothing was linked.")
    files = _plan(source)
    if not _same_volume(source, parent):
        raise LinkRefused("That copy is on another drive, and copying 13 GB isn't done here. "
                          "Move it to this Mac's main drive first.")
    tmp = parent / f".{config.MODEL_ID}.linking-{uuid.uuid4().hex[:8]}"
    try:
        tmp.mkdir()
        for real, rel in files:
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _clonefile(real, dst)
        if not _valid_model_folder(tmp):
            raise LinkRefused("The copy came out incomplete. Nothing was linked.")
        os.rename(tmp, final)
    except LinkRefused:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if exc.errno in (errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS):
            raise LinkRefused("This drive can't share files without copying them, so nothing was linked.") from None
        raise LinkRefused("Wisp couldn't link the model. Nothing was changed.") from None
    return final
