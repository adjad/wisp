"""Read-only capture of the user's Documents and Desktop files for a live run.

Stdlib only. Selects regular files under ``~/Documents`` and ``~/Desktop``
and hands them to ``snapshots.capture``, which copies each one read-only into
a fresh snapshot with a SHA-256 manifest. The host verifies that manifest
before it imports anything, and file reads stay bound to the copied home.

Excluded: hidden paths, links, application and package bundles, version
control and environment folders, the Wisp project trees (except the project
docs, minus the test corpus itself), the evaluation's own run folder,
credential and executable types (refused by ``snapshots`` as well), files
over the per-file limit, and everything past the total budget. A capture that
hit the budget says so in its manifest; it never claims to be complete.

    capture.py files --dest DIR [--home HOME] [--exclude PATH ...]
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.notch_evaluation import snapshots  # noqa: E402

ROOTS = ("Documents", "Desktop")
SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".build", "build",
                       "DerivedData", ".wisp-build", "Library"})
SKIP_DIR_SUFFIXES = (".app", ".bundle", ".framework", ".photoslibrary", ".xcodeproj", ".xcworkspace",
                     ".pkg", ".kext", ".plugin", ".savedState")
PROJECT_PREFIX = "MOE_Project"
TOTAL_BUDGET = 2 * 1024 ** 3


def real_home() -> Path:
    return Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))


def _skip_dir(rel_parts: tuple[str, ...], name: str) -> bool:
    if name.startswith(".") or name in SKIP_DIRS or name.endswith(SKIP_DIR_SUFFIXES):
        return True
    # Desktop/MOE_Project*: only MOE_Project/docs is a source.
    if len(rel_parts) == 1 and rel_parts[0] == "Desktop" and name.startswith(PROJECT_PREFIX):
        return name != PROJECT_PREFIX
    if rel_parts[:2] == ("Desktop", PROJECT_PREFIX) and len(rel_parts) == 2:
        return name != "docs"
    return False


def select_files(home: Path, *, excluded: tuple[str, ...] = (), budget: int = TOTAL_BUDGET) -> dict:
    """The relative paths to capture, and what was left out (counts only)."""
    excluded_real = {os.path.realpath(p) for p in excluded}
    chosen: list[str] = []
    skipped = {"hidden_or_excluded_dirs": 0, "links": 0, "refused_type": 0, "too_large": 0,
               "excluded_paths": 0, "project_files": 0, "over_budget": 0}
    total = 0
    for root in ROOTS:
        top = home / root
        if not top.is_dir() or top.is_symlink():
            continue
        for base, dirs, names in os.walk(top, followlinks=False):
            rel_parts = Path(base).relative_to(home).parts
            kept = []
            for name in sorted(dirs):
                path = os.path.join(base, name)
                if os.path.islink(path) or _skip_dir(rel_parts, name) or os.path.realpath(path) in excluded_real:
                    skipped["hidden_or_excluded_dirs"] += 1
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                path = os.path.join(base, name)
                rel = str(Path(path).relative_to(home))
                if name.startswith("."):
                    continue
                if rel_parts == ("Desktop", PROJECT_PREFIX):     # the project itself: only docs/
                    skipped["project_files"] += 1
                    continue
                if os.path.realpath(path) in excluded_real:
                    skipped["excluded_paths"] += 1
                    continue
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    skipped["links"] += 1
                    continue
                try:
                    snapshots._check_relative(rel)
                except snapshots.ScopeError:
                    skipped["refused_type"] += 1
                    continue
                if info.st_size > snapshots.MAX_FILE_BYTES:
                    skipped["too_large"] += 1
                    continue
                if total + info.st_size > budget:
                    skipped["over_budget"] += 1
                    continue
                total += info.st_size
                chosen.append(rel)
    return {"files": chosen, "bytes": total, "skipped": skipped,
            "complete": skipped["over_budget"] == 0}


def capture_files(dest: str | os.PathLike, *, home: Path | None = None, excluded: tuple[str, ...] = (),
                  budget: int = TOTAL_BUDGET) -> dict:
    home = home or real_home()
    selection = select_files(home, excluded=excluded, budget=budget)
    empty_state = Path(dest).with_name(Path(dest).name + ".no-state")   # never created: no caches captured
    manifest = snapshots.capture(home, empty_state, dest, snapshots.SourceScope(files=tuple(selection["files"])))
    return {"manifest_sha256": manifest["manifest_sha256"], "files": len(selection["files"]),
            "bytes": selection["bytes"], "skipped": selection["skipped"], "complete": selection["complete"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    files = sub.add_parser("files")
    files.add_argument("--dest", required=True)
    files.add_argument("--exclude", action="append", default=[])
    files.add_argument("--budget-bytes", type=int, default=TOTAL_BUDGET)
    args = parser.parse_args(argv)
    os.umask(0o077)
    result = capture_files(args.dest, excluded=tuple(args.exclude), budget=args.budget_bytes)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
