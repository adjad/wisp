"""Filesystem scopes a skill tool declares, and how they're shown to the user.

A scope is a directory the tool may read or write, written in the skill's
frontmatter as `~/Downloads` or an absolute path. Scopes exist so a generated
tool can do genuinely useful work on the user's files ("tidy my Downloads",
"rename these photos") while the user still sees, on the confirmation card,
exactly how far it can reach before approving it.

Two rules that are the whole point of the file:

**A tool cannot widen its own scope.** Scopes are read from the skill's
frontmatter and enforced by the kernel (see sandbox.py). Nothing the model
emits at call time can change them — otherwise the declaration would be a
suggestion, and the card would be describing a boundary that doesn't hold.

**Some roots are refused outright, at any breadth.** `/` and `~` as a whole
defeat the point: a card reading "this tool can write anywhere in your home
folder" is not meaningfully different from no containment, and the user cannot
reason about it. System paths are refused because a generated utility has no
legitimate reason to write there and the failure mode is an unbootable machine.
"""
from __future__ import annotations

from pathlib import Path

# Refused as scope roots — too broad to consent to meaningfully, or too
# dangerous to hand a generated script. Checked after resolution, so `~/..`
# and `/Users/<name>/../..` are caught too.
_REFUSED_ROOTS = {
    Path("/"), Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
    Path("/etc"), Path("/private/etc"), Path("/var"), Path("/private/var"),
    Path("/Library"), Path("/Applications"),
}


def _refused(path: Path) -> str:
    home = Path.home()
    if path in _REFUSED_ROOTS:
        return f"{path} is too broad or too sensitive to grant a generated tool"
    if path == home:
        return ("your whole home folder is too broad to grant — name the "
                "specific folder the tool needs (e.g. ~/Downloads)")
    if path == home.parent:                     # /Users
        return "every user's home folder is too broad to grant"
    # A scope INSIDE a system root is fine (/usr/local/share/x); a scope that
    # IS one, or contains one, is not.
    for root in _REFUSED_ROOTS:
        if root != Path("/") and root.is_relative_to(path):
            return f"{path} contains {root}, which a generated tool must not reach"
    return ""


def parse_scopes(raw: object) -> tuple[list[Path], str]:
    """Frontmatter value -> (resolved paths, error). Accepts a string or list."""
    if raw in (None, "", []):
        return [], ""
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        return [], f"expected a path or list of paths, got {type(raw).__name__}"

    out: list[Path] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser().resolve()
        except Exception as e:  # noqa: BLE001
            return [], f"bad path {text!r}: {e}"
        if (why := _refused(path)):
            return [], why
        out.append(path)
    return out, ""


def describe(read_scopes: list[Path], write_scopes: list[Path],
             sandboxed: bool = True) -> str:
    """One line for the confirmation card: what this tool can reach.

    Says "cannot read or write any of your files" for the common no-scope case
    rather than staying silent — the absence of a warning is easy to read as
    "nobody checked", and the containment is the reassuring part.
    """
    if not sandboxed:
        return ("⚠ NOT sandboxed on this system — this tool runs with your full "
                "file access. Read the code carefully.")

    def fmt(paths: list[Path]) -> str:
        home = Path.home()
        return ", ".join(
            f"~/{p.relative_to(home)}" if p.is_relative_to(home) else str(p)
            for p in paths)

    if not read_scopes and not write_scopes:
        return "Sandboxed: cannot read or write any of your files."
    # A path granted for writing is implicitly readable, so listing it under
    # both reads as two separate grants for what is one folder.
    read_only = [p for p in read_scopes if p not in write_scopes]
    parts = []
    if write_scopes:
        parts.append(f"read and modify {fmt(write_scopes)}")
    if read_only:
        parts.append(f"read {fmt(read_only)}")
    return "Sandboxed. This tool can " + "; ".join(parts) + " — nothing else."
