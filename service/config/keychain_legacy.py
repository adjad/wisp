"""A one-time copy of a saved key from its pre-rename Keychain service.

Renaming the app renamed its Keychain services, so a key saved before the rename sits
under the old service, where the renamed app no longer looks. The first time a key is
actually needed and the current service has no item for it, this copies the old item
to the current service and returns it. Reading the old item makes macOS ask the person
first ("security wants to use your confidential information..."). Nothing runs at
launch, and the old item is left in place.

The key's value is never logged, shown, written to disk, put in argv or put in an
error: it is read from `security`'s stdout and written through `security -i`'s stdin.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
import subprocess

# The Keychain service names from before the app's rename, by credential. Lines marked
# keychain-legacy-name keep their old name when the app is renamed. Where the app was
# never renamed these equal the current names and nothing is ever copied.
_PRE_RENAME = {
    "inference": "com.wisp.inference",  # keychain-legacy-name
    "study": "com.wisp.study",  # keychain-legacy-name
}

NOT_FOUND = 44   # errSecItemNotFound, from `security`
# Account names and key values that pass through `security -i` unquoted. Real keys
# (sk-or-..., AIza...) fit; anything else is not copied and the person reconnects.
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+/=:-]{0,4095}")   # never read as an option
_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
_COPYING = contextvars.ContextVar("keychain_legacy_copying", default=True)


class LegacyUnreadable(Exception):
    """An old item exists but macOS did not hand it over (refused, timed out, malformed)."""


@contextlib.contextmanager
def no_copy():
    """Inside this block a missing key is never copied (so no macOS question): the
    startup check, which nobody is waiting on."""
    token = _COPYING.set(False)
    try:
        yield
    finally:
        _COPYING.reset(token)


def legacy_service(kind: str, current: str) -> str | None:
    """The old service for this credential, or None when there is nothing to copy from."""
    old = _PRE_RENAME.get(kind)
    return old if old and old != current else None


def _run(argv: list[str], *, timeout: float, stdin: bytes | None = None):
    return subprocess.run(["/usr/bin/security", *argv], input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, env=_ENV, check=False)


def legacy_exists(kind: str, current: str, account: str, *, timeout: float = 10) -> bool:
    """Whether an old item exists, from its attributes only (no -w, so no question)."""
    old = legacy_service(kind, current)
    if old is None or not _SAFE.fullmatch(account):
        return False
    try:
        return _run(["find-generic-password", "-s", old, "-a", account], timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def copy_forward(kind: str, current: str, account: str, *, timeout: float) -> str | None:
    """The key from the old service, now also saved under `current`, or None when there
    is no old item (or copying is off here). Raises LegacyUnreadable when an old item
    exists but cannot be read. A failed write still returns the key for this use."""
    if not _COPYING.get() or not legacy_exists(kind, current, account):
        return None
    old = legacy_service(kind, current)
    try:
        read = _run(["find-generic-password", "-s", old, "-a", account, "-w"], timeout=timeout)
        value = read.stdout.decode("ascii").rstrip("\n")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        raise LegacyUnreadable from None
    if read.returncode or read.stderr or not _SAFE.fullmatch(value):
        raise LegacyUnreadable
    command = f"add-generic-password -U -s {current} -a {account} -w {value}\n"
    try:
        # `security -i` reads its command from stdin: the value never reaches argv.
        _run(["-i"], timeout=10, stdin=command.encode("ascii"))
    except (OSError, subprocess.SubprocessError):
        pass   # not saved: the next use asks again, and this use still works
    finally:
        del command
    return value
