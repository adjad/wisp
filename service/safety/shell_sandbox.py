"""Kernel-enforced write protection for Wisp's own folder, for run_shell.

run_shell hands a command to /bin/sh. The policy (service/safety/policy.py) reads the
command's TEXT and asks first when it names one of Wisp's own files, but text is all it
can read: a name built at run time (a shell variable, an interpreter, find's own matching,
`$'\\x2e'`) never appears in it. With Full access on, such a command could write
`act_for_me.json` (the person's yes to "Let Wisp act for me"), `grants.json` or
`access_mode.json` with no card at all.

So the command runs under macOS's `sandbox-exec` with one rule: no writing inside Wisp's
data folder or the Keychains folder. The kernel refuses the write whatever the path was
spelled like, including through a symlink, a hard link, `..`, another letter case or the
/System/Volumes/Data spelling, and for every process the command starts. Everything else
(reads, the network, writes anywhere else) is as before.

What was checked on macOS 27.0.1, and is pinned by tests/test_shell_sandbox.py:
- a write-deny on a folder also refuses creating, deleting, renaming, chmod and touch
  inside it, renaming the folder itself, and making a hard link to a file in it;
- it does NOT stop a PARENT folder being renamed, which would leave Wisp's path pointing
  at a fresh folder anyone can fill. Every parent folder is therefore denied by its exact
  path; that stops the rename and does not stop files being made in the parent.

What this does not cover:
- a command that hands the work to a process the shell did not start: `osascript` and
  `shortcuts` (always confirmed), `open`, `launchctl`. Those run outside the sandbox;
- a command that needs a sandbox of its own (`swift build` without `--disable-sandbox`,
  Homebrew builds from source, other agent CLIs): macOS does not nest sandboxes, so it
  fails here. explain() tells the model why;
- a Wisp backend that is itself sandboxed (Simulation QA, some CI): `sandbox-exec` cannot
  be applied there, usable() is False and the command runs as it did before.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from service.skills.sandbox import SANDBOX_EXEC, _q

log = logging.getLogger(__name__)

# Only a success is remembered. A failure is checked again at the next command, so one slow
# moment cannot leave the shell unprotected for the rest of the run.
_usable = False
_warned = False


def _ask() -> bool:
    """Start a fixed command under an empty sandbox profile; True when it ran. Popen and not
    subprocess.run, so the answer never comes from whatever stands in for a caller's run()."""
    try:
        probe = subprocess.Popen([SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
    except (OSError, ValueError):
        return False
    try:
        return probe.wait(timeout=10) == 0
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.wait()
        return False


def usable() -> bool:
    """Whether a sandbox can be applied to a child of this process. It is asked with a fixed
    command, never by looking at how the user's command failed: a command could fake that."""
    global _usable, _warned
    if not _usable:
        _usable = _ask()
        if not _usable and not _warned:
            _warned = True
            log.warning("sandbox-exec cannot be applied here; run_shell runs without the write guard")
    return _usable


def protected_roots() -> list[Path]:
    """The folders no shell command may change: Wisp's data folder (where WISP_HOME points,
    and the standard one), and the login Keychains."""
    from service.paths import MOE_DIR
    home = Path.home()
    return list(dict.fromkeys((Path(MOE_DIR), home / ".moe", home / "Library" / "Keychains")))


def _spellings(root: Path) -> list[Path]:
    """`root` as an absolute path, and as it resolves on disk. The kernel judges the resolved
    path; the path as written is kept too, so a symlink on the way cannot be swapped."""
    written = Path(os.path.abspath(root))
    try:
        real = Path(os.path.realpath(written))
    except (OSError, ValueError):
        real = written
    return list(dict.fromkeys((written, real)))


def build_profile() -> str:
    """The sandbox profile: everything allowed, except writing under protected_roots()."""
    folders, parents = [], []
    for root in protected_roots():
        for path in _spellings(root):
            folders.append(path)
            parents.extend(parent for parent in path.parents if str(parent) != "/")
    lines = ["(version 1)", "(allow default)"]
    lines += [f'(deny file-write* (subpath "{_q(path)}"))' for path in dict.fromkeys(folders)]
    # A parent's exact path only: renaming or replacing it is refused, files in it are not.
    lines += [f'(deny file-write* (literal "{_q(path)}"))'
              for path in dict.fromkeys(parents) if path not in folders]
    return "\n".join(lines) + "\n"


def contain(argv: list[str]) -> tuple[list[str], bool]:
    """`argv` to run under the write guard, and whether the guard is on."""
    if not usable():
        return list(argv), False
    return [SANDBOX_EXEC, "-p", build_profile(), *argv], True


_NOTE = ("[note] Wisp's shell cannot change anything inside Wisp's own data folder or the "
         "Keychains folder, and a command run from it cannot start a sandbox of its own "
         "(`swift build` needs --disable-sandbox). \"Operation not permitted\" above may be "
         "that guard. Do not retry it another way; tell the person what was refused.")


def explain(contained: bool, returncode: int, stderr: str | None) -> str:
    """A line for the model when a failed command may have met the guard, else ""."""
    if not contained or returncode == 0 or not stderr:
        return ""
    return _NOTE if ("Operation not permitted" in stderr or "sandbox_apply" in stderr) else ""
