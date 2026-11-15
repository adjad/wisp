"""Read origin-bound provider secrets from macOS Keychain without secret argv."""
import asyncio
import hashlib
import platform
import subprocess
import threading
import time

from . import keychain_legacy, quarantine

SERVICE = "com.wisp.inference"

# The first read of a key the app saved makes macOS ask the person ("security wants to
# use your confidential information stored in com.wisp.inference"), and the question
# needs the login password. The read waits long enough to answer it (Always Allow makes
# later reads silent). Only one read runs at a time, so only one question is ever on
# screen: a read that finds another in progress waits a few seconds, then fails instead
# of queueing behind a question (no pile of waiting threads, no stack of dialogs). A
# refused or failed read is remembered for a short while per account, so status polls
# don't put the question straight back.
READ_TIMEOUT_S = 60.0
LOCK_WAIT_S = 5.0
CONNECT_LOCK_WAIT_S = 10.0
FAILURE_HOLD_S = 20.0
_READ_LOCK = threading.Lock()
_RECENT_FAILURE: dict[str, float] = {}
# Accounts whose last failed read found no key at all (not one macOS refused to hand over).
_RECENT_MISSING: set[str] = set()
# Intel only (every answer is a cloud call): a key read in the last few minutes is used
# again instead of running `security` before each turn's first token. Connect (`wait`)
# always reads Keychain afresh, and a Connect restarts the service, which clears this;
# saving or removing a cloud connection also forgets it (forget_reused_keys).
SUCCESS_REUSE_S = 300.0
_RECENT_SUCCESS: dict[str, tuple[float, str]] = {}


def forget_reused_keys() -> None:
    """Drop every key held for reuse: the next read goes to Keychain again."""
    _RECENT_SUCCESS.clear()


def _reuse_enabled() -> bool:
    return platform.machine() == "x86_64"


def keychain_account(name: str, base_url: str) -> str:
    # A copied credential reference cannot authorize a different destination.
    digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
    return f"{name}:{digest}"


def resolve_keychain(name: str, base_url: str, *, wait: bool = False) -> str:
    """The key for name at base_url. `wait` (Connect only) waits for a read already in
    progress and ignores a remembered failure: the person is at the screen to answer."""
    from .endpoints import (ProviderCredentialBusy, ProviderCredentialCopyPending,
                            ProviderCredentialError, ProviderCredentialTimedOut)
    quarantine.check()
    account = keychain_account(name, base_url)
    # A silent read (after Always Allow) takes well under a second, so overlapping reads
    # wait their turn briefly; only a read stuck on macOS's question makes them fail.
    # Connect waits longer; the app's Connect request allows for this wait, the read and its tests.
    # Called on the event loop itself (a few synchronous callers), it never waits: the
    # whole service would stall behind macOS's question.
    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False
    if not wait and _reuse_enabled():
        held = _RECENT_SUCCESS.get(account)
        if held is not None and time.monotonic() - held[0] < SUCCESS_REUSE_S:
            return held[1]
    limit = CONNECT_LOCK_WAIT_S if wait else (0 if on_loop else LOCK_WAIT_S)
    if not _READ_LOCK.acquire(timeout=limit):
        raise ProviderCredentialBusy("Provider credential unavailable")
    try:
        failed = _RECENT_FAILURE.get(account)
        if not wait and failed is not None and time.monotonic() - failed < FAILURE_HOLD_S:
            raise _failure(account)
        timeout = 10 if on_loop else READ_TIMEOUT_S
        missing = False
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", SERVICE,
                 "-a", account, "-w"],
                # On the event loop the whole service waits on this, so never long.
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
            )
            quarantine.check()
            if result.returncode == keychain_legacy.NOT_FOUND:
                # Saved before the app's rename? Copy it across once (macOS asks first),
                # but only off the event loop and outside the startup check. There a key
                # waiting to be copied is neither missing nor held.
                if on_loop or not keychain_legacy.copying():
                    if keychain_legacy.legacy_exists("inference", SERVICE, account):
                        raise ProviderCredentialCopyPending("Provider credential unavailable")
                    value = None
                else:
                    value = keychain_legacy.copy_forward("inference", SERVICE, account)
                missing = value is None
                if missing:
                    raise ValueError
            else:
                value = result.stdout.decode("ascii").rstrip("\n")
                if result.returncode or result.stderr:
                    raise ValueError
            if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError
        except ProviderCredentialCopyPending:
            raise
        except keychain_legacy.LegacyTimedOut:
            # Unanswered, not refused: no hold, so the next use asks again.
            raise ProviderCredentialTimedOut("Provider credential unavailable") from None
        except (OSError, subprocess.SubprocessError, ValueError, keychain_legacy.LegacyUnreadable):
            _RECENT_FAILURE[account] = time.monotonic()
            _RECENT_SUCCESS.pop(account, None)
            if missing:
                _RECENT_MISSING.add(account)
            else:
                _RECENT_MISSING.discard(account)
            raise _failure(account) from None
        _RECENT_FAILURE.pop(account, None)
        _RECENT_MISSING.discard(account)
        if _reuse_enabled():
            _RECENT_SUCCESS[account] = (time.monotonic(), value)
        return value
    finally:
        _READ_LOCK.release()


def _failure(account: str) -> Exception:
    """The error for a failed read: missing (nothing saved, so reconnect) or unreadable.
    Its text never names the key, the account or the service."""
    from .endpoints import ProviderCredentialError, ProviderCredentialMissing
    if account in _RECENT_MISSING:
        return ProviderCredentialMissing("Provider credential unavailable")
    return ProviderCredentialError("Provider credential unavailable")
