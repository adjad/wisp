"""Kernel-enforced filesystem containment for generated / installed skill tools.

Skill tools were only ever contained on paper. `_make_runner` checks that the
SCRIPT FILE lives inside its own skill folder — but once that script starts it
is an ordinary child process of a non-App-Sandboxed app, so it inherits the
user's full filesystem rights. A generated tool asked to "tidy my Downloads"
could just as easily read ~/.ssh or empty ~/Documents, and the only thing
standing between the user and that was reading the code on the confirmation
card. That is a review step, not a boundary.

macOS's `sandbox-exec` gives a real one: the kernel refuses the syscall, so it
holds regardless of what the generated code actually does — including `../..`
traversal, `os.remove`, and subprocesses it spawns itself.

The profile is deny-by-default for WRITES with an explicit allowlist, and
denies READS across the user's home except for the scopes the tool declared.
Everything else (interpreter startup, dyld cache, /usr, /System) stays allowed,
because locking those down stops Python from booting at all — verified: a
`(deny default)` profile kills the interpreter with SIGABRT before it runs a
line, which reads as "the tool is broken" rather than "the tool was contained".

`sandbox-exec` is formally deprecated by Apple but is still present and
functional on macOS 27, and is the same mechanism Chrome and others rely on.
If a future OS removes it, `available()` returns False and skill tools fall
back to running unsandboxed — the confirmation card says so explicitly rather
than implying a containment that isn't there.
"""
from __future__ import annotations

import shutil
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Always writable: the tool's own folder needs scratch space, and these device
# nodes / temp dirs are required for ordinary stdio and tempfile use.
_ALWAYS_WRITE = [
    '(literal "/dev/null")', '(literal "/dev/stdout")', '(literal "/dev/stderr")',
    '(literal "/dev/dtracehelper")', '(subpath "/private/var/folders")',
]

# Where personal data actually lives. Reads are denied across these roots and
# re-allowed only for declared scopes.
#
# This is a deny-list rather than the stricter deny-everything-then-allow, for
# an empirical reason: a global `(deny file-read*)` — even `file-read-data`
# alone, with /usr, /System, /Library, /opt and the dyld cache explicitly
# allowed back — kills the interpreter with SIGABRT before it executes a line.
# The user sees "the tool is broken", not "the tool was contained". These four
# roots cover every location user data realistically sits in (all home folders,
# mounted external and network volumes, the shared temp dirs, root's home);
# what stays readable is OS and application code, which holds nothing personal.
_READ_DENY_ROOTS = [
    "/Users", "/Volumes", "/private/tmp", "/private/var/root",
]

# Read-denied even when a broader scope is granted. A tool given ~/Documents
# has no business in the user's keys or Wisp's own credential/session stores,
# and these are the paths where a mistake is unrecoverable rather than annoying.
_NEVER_READ = [
    "~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/Library/Keychains",
    "~/.moe/sessions.db", "~/.moe/facts.db", "~/.moe/assistant.db",
]


def available() -> bool:
    return Path(SANDBOX_EXEC).exists() and shutil.which("sandbox-exec") is not None


def _q(path: Path) -> str:
    """A path as a sandbox-profile string literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_profile(skill_dir: Path, read_scopes: list[Path],
                  write_scopes: list[Path]) -> str:
    """A sandbox profile allowing reads/writes only within the given scopes.

    The skill's own folder is always readable and writable — that's where the
    script itself lives and where it may keep state.
    """
    home = Path.home()
    # write scopes imply read — a tool that can modify a folder can obviously
    # see it, and declaring both separately for the same path is pure friction.
    readable = list(dict.fromkeys([*read_scopes, *write_scopes]))
    lines = [
        "(version 1)",
        "(allow default)",
        # --- writes: deny everything, then re-allow the declared scopes ---
        "(deny file-write*)",
        *(f"(allow file-write* {frag})" for frag in _ALWAYS_WRITE),
        f'(allow file-write* (subpath "{_q(skill_dir.resolve())}"))',
        *(f'(allow file-write* (subpath "{_q(p)}"))' for p in write_scopes),
        # --- reads: deny the user-data roots, re-allow declared scopes ---
        *(f'(deny file-read* (subpath "{_q(Path(p))}"))' for p in _READ_DENY_ROOTS),
        f'(allow file-read* (subpath "{_q(skill_dir.resolve())}"))',
        *(f'(allow file-read* (subpath "{_q(p)}"))' for p in readable),
    ]
    # Re-deny the sensitive paths LAST so they win even if a granted scope
    # (e.g. all of ~) would otherwise cover them. In SBPL the last matching
    # rule applies, so ordering here is load-bearing, not stylistic.
    for raw in _NEVER_READ:
        p = Path(raw.replace("~", str(home)))
        lines.append(f'(deny file-read* (subpath "{_q(p)}"))')
        lines.append(f'(deny file-write* (subpath "{_q(p)}"))')
    return "\n".join(lines) + "\n"


def wrap_argv(argv: list[str], profile_path: Path) -> list[str]:
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]
